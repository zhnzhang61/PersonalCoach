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

const config = {
  hrv: { label: "HRV (ms)", color: "var(--chart-1)" },
  sleep_score: { label: "Sleep score", color: "var(--chart-2)" },
} satisfies ChartConfig;

interface Props {
  days?: number;
}

export function TimelineChart({ days = 30 }: Props) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["health", "timeline", days],
    queryFn: () =>
      apiGet<HealthTimelineResponse>(`/api/health/timeline?days=${days}`),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base font-medium">
          {days}-day trend
        </CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-[220px] w-full" />
        ) : error ? (
          <div className="text-sm text-rose-600 dark:text-rose-400">
            Failed to load timeline: {(error as Error).message}
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
                yAxisId="sleep"
                orientation="right"
                tickLine={false}
                axisLine={false}
                tickMargin={4}
                width={28}
                domain={[0, 100]}
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
                yAxisId="sleep"
                dataKey="sleep_score"
                type="monotone"
                stroke="var(--color-sleep_score)"
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
