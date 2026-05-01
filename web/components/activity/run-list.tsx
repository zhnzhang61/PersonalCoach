"use client";

import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/lib/api";
import type {
  ManualActivitiesResponse,
  RunActivity,
  RunsResponse,
} from "@/lib/types";
import { Skeleton } from "@/components/ui/skeleton";
import { RunCard } from "@/components/activity/run-card";
import { ManualActivityCard } from "@/components/activity/manual-activity-card";
import { useCurrentWeek } from "@/lib/hooks/use-current-week";

type RunItem = { kind: "run"; date: string; payload: RunActivity };
type ManualItem = {
  kind: "manual";
  date: string;
  payload: import("@/lib/types").ManualActivity;
};
type FeedItem = RunItem | ManualItem;

export function RunList() {
  const { week, isLoading: weekLoading } = useCurrentWeek();
  const range = week ? `start=${week.start}&end=${week.end}` : null;

  const runsQuery = useQuery({
    queryKey: ["runs", week?.start, week?.end],
    queryFn: () => apiGet<RunsResponse>(`/api/runs?${range}`),
    enabled: !!range,
  });

  const manualQuery = useQuery({
    queryKey: ["manual-activities", week?.start, week?.end],
    queryFn: () =>
      apiGet<ManualActivitiesResponse>(`/api/manual-activities?${range}`),
    enabled: !!range,
  });

  if (weekLoading || runsQuery.isLoading || manualQuery.isLoading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }

  const runs = runsQuery.data?.runs ?? [];
  const manuals = manualQuery.data?.activities ?? [];

  const items: FeedItem[] = [
    ...runs.map<RunItem>((r) => ({
      kind: "run",
      date: r.startTimeLocal?.slice(0, 10) ?? "",
      payload: r,
    })),
    ...manuals.map<ManualItem>((a) => ({
      kind: "manual",
      date: a.date,
      payload: a,
    })),
  ].sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));

  if (items.length === 0) {
    return (
      <div className="rounded-md border border-border bg-muted/30 p-6 text-center text-sm text-muted-foreground">
        No activities logged this week yet.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {items.map((item) =>
        item.kind === "run" ? (
          <RunCard key={item.payload.activityId} run={item.payload} />
        ) : (
          <ManualActivityCard key={item.payload.id} activity={item.payload} />
        ),
      )}
    </div>
  );
}
