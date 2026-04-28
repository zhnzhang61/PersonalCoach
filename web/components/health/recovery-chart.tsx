"use client";

import { useQuery } from "@tanstack/react-query";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
  XAxis,
  YAxis,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet } from "@/lib/api";
import { fmtDate } from "@/lib/format";
import type { HealthSnapshot, HealthTimelineResponse } from "@/lib/types";

// HRV and RHR plotted together — both natural axes (no inversion). The user
// reads "HRV high = good, RHR low = good" themselves. The shaded green band on
// the HRV axis is Garmin's currently-calibrated balanced range, so historical
// HRV values can be visually compared against today's normal zone.
const config = {
  hrv: { label: "HRV (ms)", color: "var(--chart-1)" },
  rhr: { label: "Resting HR (bpm)", color: "var(--chart-2)" },
} satisfies ChartConfig;

interface Props {
  days?: number;
}

export function RecoveryChart({ days = 30 }: Props) {
  const timeline = useQuery({
    queryKey: ["health", "timeline", days],
    queryFn: () =>
      apiGet<HealthTimelineResponse>(`/api/health/timeline?days=${days}`),
  });
  const snapshot = useQuery({
    queryKey: ["health", "snapshot", 14],
    queryFn: () => apiGet<HealthSnapshot>("/api/health/snapshot?baseline_days=14"),
  });

  const hrvBand = snapshot.data?.metrics
    .find((m) => m.key === "hrv")
    ?.context;
  const balancedLow =
    hrvBand?.type === "hrv_band" ? hrvBand.balanced_low : null;
  const balancedUpper =
    hrvBand?.type === "hrv_band" ? hrvBand.balanced_upper : null;

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="eyebrow">Recovery composite</div>
        <CardTitle className="font-heading text-xl font-semibold tracking-tight sm:text-2xl">
          {days}-day HRV vs Resting HR
        </CardTitle>
      </CardHeader>
      <CardContent>
        {timeline.isLoading ? (
          <Skeleton className="h-[220px] w-full" />
        ) : timeline.error ? (
          <div className="text-sm text-rose-600 dark:text-rose-400">
            Failed to load: {(timeline.error as Error).message}
          </div>
        ) : (
          <ChartContainer config={config} className="h-[220px] w-full">
            <LineChart
              data={timeline.data?.timeline ?? []}
              margin={{ left: 4, right: 12, top: 8, bottom: 4 }}
            >
              <CartesianGrid strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="date"
                tickLine={false}
                axisLine={false}
                tickMargin={8}
                minTickGap={32}
                tickFormatter={(v: string) => fmtDate(v)}
              />
              <YAxis
                yAxisId="hrv"
                orientation="left"
                tickLine={false}
                axisLine={false}
                tickMargin={4}
                width={28}
                domain={["dataMin - 5", "dataMax + 5"]}
              />
              <YAxis
                yAxisId="rhr"
                orientation="right"
                tickLine={false}
                axisLine={false}
                tickMargin={4}
                width={28}
                domain={["dataMin - 2", "dataMax + 2"]}
              />
              {balancedLow != null && balancedUpper != null && (
                <ReferenceArea
                  yAxisId="hrv"
                  y1={balancedLow}
                  y2={balancedUpper}
                  fill="var(--color-warm-accent)"
                  fillOpacity={0.12}
                  stroke="var(--color-warm-accent)"
                  strokeOpacity={0.25}
                  strokeDasharray="3 3"
                  ifOverflow="extendDomain"
                />
              )}
              <ChartTooltip
                content={
                  <ChartTooltipContent
                    labelFormatter={(v) => fmtDate(v as string, "EEE, MMM d")}
                  />
                }
              />
              <Line
                yAxisId="hrv"
                dataKey="hrv"
                type="monotone"
                stroke="var(--color-hrv)"
                strokeWidth={2}
                dot={false}
              />
              <Line
                yAxisId="rhr"
                dataKey="rhr"
                type="monotone"
                stroke="var(--color-rhr)"
                strokeWidth={2}
                dot={false}
              />
              <ChartLegend content={<ChartLegendContent />} />
            </LineChart>
          </ChartContainer>
        )}
        {balancedLow != null && balancedUpper != null && (
          <p className="mt-2 text-[10px] text-muted-foreground">
            Shaded band = Garmin&rsquo;s balanced HRV range ({balancedLow}–
            {balancedUpper} ms).
          </p>
        )}
      </CardContent>
    </Card>
  );
}
