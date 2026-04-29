"use client";

import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/lib/api";
import type { CycleStatsResponse } from "@/lib/types";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useCurrentWeek } from "@/lib/hooks/use-current-week";

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="eyebrow text-[9px]">{label}</span>
      <span className="font-heading text-lg font-semibold tabular-nums leading-none">
        {value}
      </span>
      {hint && <span className="text-[10px] text-muted-foreground">{hint}</span>}
    </div>
  );
}

export function WeekBanner() {
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
        <CardContent className="p-4">
          <Skeleton className="h-12 w-full" />
        </CardContent>
      </Card>
    );
  }

  const wk = statsQuery.data?.week;
  if (!wk) {
    return (
      <Card>
        <CardContent className="p-4 text-xs text-muted-foreground">
          No data for this week yet.
        </CardContent>
      </Card>
    );
  }

  const vsLabel =
    wk.vs_avg === 0
      ? "on cycle avg"
      : `${wk.vs_avg > 0 ? "+" : ""}${wk.vs_avg.toFixed(1)} mi vs avg`;

  return (
    <Card>
      <CardContent className="grid grid-cols-3 gap-3 p-4 sm:grid-cols-5">
        <Stat label="Runs" value={`${wk.runs}`} />
        <Stat label="Miles" value={wk.miles.toFixed(1)} hint={vsLabel} />
        <Stat label="Pace" value={wk.avg_pace} hint="min/mi" />
        <Stat label="Avg HR" value={wk.avg_hr > 0 ? `${wk.avg_hr}` : "—"} />
        <Stat
          label="Elev"
          value={wk.elevation_ft > 0 ? `${wk.elevation_ft.toLocaleString()}` : "—"}
          hint="ft"
        />
      </CardContent>
    </Card>
  );
}
