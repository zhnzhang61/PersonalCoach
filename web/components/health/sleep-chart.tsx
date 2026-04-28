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
  sleep_score: { label: "Sleep score", color: "var(--chart-3)" },
  sleep_hours: { label: "Hours", color: "var(--chart-4)" },
} satisfies ChartConfig;

interface Props {
  days?: number;
}

export function SleepChart({ days = 30 }: Props) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["health", "timeline", days],
    queryFn: () =>
      apiGet<HealthTimelineResponse>(`/api/health/timeline?days=${days}`),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="eyebrow">Sleep quality</div>
        <CardTitle className="font-heading text-xl font-semibold tracking-tight sm:text-2xl">
          {days}-day score vs hours
        </CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-[200px] w-full" />
        ) : error ? (
          <div className="text-sm text-rose-600 dark:text-rose-400">
            Failed to load: {(error as Error).message}
          </div>
        ) : (
          <ChartContainer config={config} className="h-[200px] w-full">
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
                yAxisId="score"
                orientation="left"
                tickLine={false}
                axisLine={false}
                tickMargin={4}
                width={28}
                domain={[0, 100]}
              />
              <YAxis
                yAxisId="hours"
                orientation="right"
                tickLine={false}
                axisLine={false}
                tickMargin={4}
                width={28}
                domain={["dataMin - 1", "dataMax + 1"]}
              />
              <ChartTooltip
                content={
                  <ChartTooltipContent
                    labelFormatter={(v) => fmtDate(v as string, "EEE, MMM d")}
                  />
                }
              />
              <Line
                yAxisId="score"
                dataKey="sleep_score"
                type="monotone"
                stroke="var(--color-sleep_score)"
                strokeWidth={2}
                dot={false}
              />
              <Line
                yAxisId="hours"
                dataKey="sleep_hours"
                type="monotone"
                stroke="var(--color-sleep_hours)"
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
