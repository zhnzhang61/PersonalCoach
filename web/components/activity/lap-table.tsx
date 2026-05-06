"use client";

import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import type { Lap, LapsResponse } from "@/lib/types";

function metersToMi(m: number): number {
  return m / 1609.34;
}

function paceForLap(lap: Lap): string {
  if (lap.distance <= 0 || lap.duration <= 0) return "—";
  const dec = lap.duration / 60 / metersToMi(lap.distance);
  return `${Math.floor(dec)}:${Math.floor((dec % 1) * 60)
    .toString()
    .padStart(2, "0")}`;
}

// Read-only view of per-lap splits (Mi / Pace / HR / Effort). Rendered on
// the run detail page below the telemetry charts so the user can see lap
// data without opening the editor. Editing still lives in EditRunForm —
// when that saves, react-query invalidates the ["runs"] prefix and this
// component refetches automatically.
export function LapTable({ activityId }: { activityId: number }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["runs", activityId, "laps"],
    queryFn: () => apiGet<LapsResponse>(`/api/runs/${activityId}/laps`),
  });

  if (isLoading) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-8 w-full" />
        <Skeleton className="h-8 w-full" />
        <Skeleton className="h-8 w-full" />
      </div>
    );
  }

  if (isError || !data || data.laps.length === 0) {
    return (
      <div className="rounded-md border border-border bg-muted/30 p-3 text-center text-xs text-muted-foreground">
        No lap data for this run.
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-md border border-border">
      <table className="w-full text-left text-sm">
        <thead className="bg-muted/40 text-xs uppercase tracking-wide text-muted-foreground">
          <tr>
            <th className="py-2 pl-3 pr-2 font-medium">Lap</th>
            <th className="py-2 pr-2 text-right font-medium">Mi</th>
            <th className="py-2 pr-2 text-right font-medium">Pace</th>
            <th className="py-2 pr-2 text-right font-medium">HR</th>
            <th className="py-2 pr-3 font-medium">Effort</th>
          </tr>
        </thead>
        <tbody>
          {data.laps.map((lap, i) => (
            <tr key={i} className="border-t border-border/50">
              <td className="py-2 pl-3 pr-2 tabular-nums">{i + 1}</td>
              <td className="py-2 pr-2 text-right tabular-nums">
                {metersToMi(lap.distance).toFixed(2)}
              </td>
              <td className="py-2 pr-2 text-right tabular-nums">
                {paceForLap(lap)}
              </td>
              <td className="py-2 pr-2 text-right tabular-nums">
                {lap.averageHR ?? "—"}
              </td>
              <td className="py-2 pr-3">
                <Badge variant="outline" className="text-[11px] font-normal">
                  {lap.category}
                </Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
