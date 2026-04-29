"use client";

import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, Cell, XAxis, YAxis } from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet } from "@/lib/api";
import type { CycleStatsResponse } from "@/lib/types";
import { useCurrentWeek } from "@/lib/hooks/use-current-week";

const chartConfig = {
  miles: { label: "Miles", color: "var(--chart-1)" },
} satisfies ChartConfig;

function StatTile({
  label,
  value,
  unit,
}: {
  label: string;
  value: string | number;
  unit?: string;
}) {
  return (
    <div className="rounded-md bg-muted/30 p-3">
      <p className="eyebrow text-[10px]">{label}</p>
      <p className="mt-1 font-heading text-xl font-semibold tabular-nums leading-none">
        {value}
        {unit && (
          <span className="ml-1 text-xs font-medium text-muted-foreground">
            {unit}
          </span>
        )}
      </p>
    </div>
  );
}

export function CycleOverview() {
  const { blockId, week, isLoading } = useCurrentWeek();

  const statsQuery = useQuery({
    queryKey: ["training", "cycle-stats", blockId, week?.start, week?.end],
    queryFn: () =>
      apiGet<CycleStatsResponse>(
        `/api/training/cycle-stats?block_id=${encodeURIComponent(blockId!)}` +
          `&week_start=${week!.start}&week_end=${week!.end}`,
      ),
    enabled: !!blockId && !!week,
  });

  if (isLoading || statsQuery.isLoading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>This Cycle</CardTitle>
        </CardHeader>
        <CardContent>
          <Skeleton className="h-48 w-full" />
        </CardContent>
      </Card>
    );
  }

  const data = statsQuery.data;
  if (!data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>This Cycle</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          No cycle data yet.
        </CardContent>
      </Card>
    );
  }

  const cy = data.cycle;
  const currentLabel = `W${data.week.week_num}`;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{data.block_name}</CardTitle>
        <p className="text-xs text-muted-foreground">This Cycle</p>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <StatTile label="Total Miles" value={cy.total_miles} />
          <StatTile label="Runs" value={cy.total_runs} />
          <StatTile label="Hours" value={cy.total_hours} />
          <StatTile label="Avg/Week" value={cy.avg_weekly_miles} unit="mi" />
          <StatTile label="Avg Pace" value={cy.avg_pace} unit="/mi" />
          <StatTile label="Avg HR" value={cy.avg_hr || "—"} />
          <StatTile
            label="Elevation"
            value={cy.elevation_ft.toLocaleString()}
            unit="ft"
          />
          <StatTile label="Longest" value={cy.longest_run} unit="mi" />
        </div>

        {cy.category_breakdown.length > 0 && (
          <div>
            <p className="eyebrow mb-2 text-[10px]">Effort Distribution</p>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="text-muted-foreground">
                  <tr className="border-b border-border">
                    <th className="py-1.5 pr-2 font-medium">Effort</th>
                    <th className="py-1.5 pr-2 text-right font-medium">Mi</th>
                    <th className="py-1.5 pr-2 text-right font-medium">%</th>
                    <th className="py-1.5 pr-2 text-right font-medium">Pace</th>
                    <th className="py-1.5 pr-2 text-right font-medium">HR</th>
                    <th className="py-1.5 text-right font-medium">Elev</th>
                  </tr>
                </thead>
                <tbody>
                  {cy.category_breakdown.map((row) => (
                    <tr key={row.effort} className="border-b border-border/50">
                      <td className="py-1.5 pr-2">{row.effort}</td>
                      <td className="py-1.5 pr-2 text-right tabular-nums">
                        {row.miles.toFixed(1)}
                      </td>
                      <td className="py-1.5 pr-2 text-right tabular-nums">
                        {row.pct_of_total.toFixed(0)}%
                      </td>
                      <td className="py-1.5 pr-2 text-right tabular-nums">
                        {row.avg_pace}
                      </td>
                      <td className="py-1.5 pr-2 text-right tabular-nums">
                        {row.avg_hr ?? "—"}
                      </td>
                      <td className="py-1.5 text-right tabular-nums">
                        {row.elevation_ft != null
                          ? `${row.elevation_ft.toLocaleString()}`
                          : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {data.weekly_miles.length > 0 && (
          <div>
            <p className="eyebrow mb-2 text-[10px]">Weekly Mileage</p>
            <ChartContainer config={chartConfig} className="h-44 w-full">
              <BarChart data={data.weekly_miles}>
                <CartesianGrid vertical={false} strokeDasharray="3 3" />
                <XAxis
                  dataKey="label"
                  tickLine={false}
                  axisLine={false}
                  tickMargin={6}
                  fontSize={10}
                />
                <YAxis
                  tickLine={false}
                  axisLine={false}
                  tickMargin={6}
                  fontSize={10}
                />
                <ChartTooltip content={<ChartTooltipContent />} />
                <Bar dataKey="miles" radius={[3, 3, 0, 0]}>
                  {data.weekly_miles.map((w) => (
                    <Cell
                      key={w.label}
                      fill={
                        w.label === currentLabel
                          ? "var(--chart-2)"
                          : "var(--chart-1)"
                      }
                    />
                  ))}
                </Bar>
              </BarChart>
            </ChartContainer>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
