"use client";

import { useQuery } from "@tanstack/react-query";
import { CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";
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
import type { HealthTimelineResponse } from "@/lib/types";

// HRV (higher = recovered) and RHR (lower = recovered) plotted together because
// when recovery is consistent they should *move in sync* once axes are aligned
// to "good = up". So RHR is rendered with an inverted Y axis. Divergence is the
// signal: HRV up + RHR up = something stressing the body; HRV down + RHR down
// = atypical recovery profile. This is what AI consumers (and the user) should
// be looking for.
const config = {
  hrv: { label: "HRV (ms)", color: "var(--chart-1)" },
  rhr: { label: "Resting HR (bpm, inverted)", color: "var(--chart-2)" },
} satisfies ChartConfig;

interface Props {
  days?: number;
}

export function RecoveryChart({ days = 30 }: Props) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["health", "timeline", days],
    queryFn: () =>
      apiGet<HealthTimelineResponse>(`/api/health/timeline?days=${days}`),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="eyebrow">Recovery composite</div>
        <CardTitle className="font-heading text-xl font-semibold tracking-tight sm:text-2xl">
          {days}-day HRV vs Resting HR
        </CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-[220px] w-full" />
        ) : error ? (
          <div className="text-sm text-rose-600 dark:text-rose-400">
            Failed to load: {(error as Error).message}
          </div>
        ) : (
          <ChartContainer config={config} className="h-[220px] w-full">
            <LineChart
              data={data?.timeline ?? []}
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
                reversed
                domain={["dataMin - 2", "dataMax + 2"]}
              />
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
      </CardContent>
    </Card>
  );
}
