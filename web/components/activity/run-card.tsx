"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import dynamic from "next/dynamic";
import { apiGet } from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { fmtDate } from "@/lib/format";
import type { RunActivity, WeatherSnapshot } from "@/lib/types";

// react-leaflet imports leaflet at module load, and leaflet touches `window`
// during init — so we hold it back from SSR. Skeleton during hydration.
const RunMap = dynamic(
  () => import("@/components/activity/run-map").then((m) => m.RunMap),
  {
    ssr: false,
    loading: () => <Skeleton className="h-64 w-full" />,
  },
);

function metersToMi(m?: number): number {
  return (m ?? 0) / 1609.34;
}

export function RunCard({ run }: { run: RunActivity }) {
  const meta = run.manual_meta ?? {};
  const name = meta.name || run.activityName || "Run";
  const dateStr = run.startTimeLocal?.slice(0, 10);
  const distMi = metersToMi(run.distance);
  const elevFt = Math.round((run.elevationGain ?? 0) * 3.281);
  const breakdown = meta.category_stats ?? [];

  const weatherQuery = useQuery({
    queryKey: ["runs", run.activityId, "weather"],
    queryFn: () => apiGet<WeatherSnapshot>(`/api/runs/${run.activityId}/weather`),
    staleTime: Infinity,
    retry: false,
  });
  const w = weatherQuery.data;

  // Drop "feels like" if it's within ~2°F of the dry temp — same threshold
  // as the old WeatherStrip used.
  const showFeels =
    w?.apparent_temperature_f != null &&
    w.temperature_f != null &&
    Math.abs(w.apparent_temperature_f - w.temperature_f) >= 2;

  const datePart = dateStr ? fmtDate(dateStr, "EEE MMM d") : "—";
  const weatherSegments: string[] = [];
  if (w?.temperature_f != null) {
    weatherSegments.push(
      showFeels
        ? `${Math.round(w.temperature_f)}°F (feels ${Math.round(w.apparent_temperature_f!)}°F)`
        : `${Math.round(w.temperature_f)}°F`,
    );
  }
  if (w?.humidity_pct != null) weatherSegments.push(`${w.humidity_pct}% humidity`);

  return (
    <Link
      href={`/activity/${run.activityId}`}
      className="block rounded-xl transition-colors hover:bg-muted/30 focus:outline-none focus-visible:ring-2 focus-visible:ring-warm-accent/40"
    >
      <Card>
        <CardContent className="flex flex-col gap-3 p-4">
          <div className="min-w-0 space-y-0.5">
            <h3 className="truncate text-base font-semibold">{name}</h3>
            <p className="text-sm text-muted-foreground">
              {[datePart, ...weatherSegments].join(" · ")}
            </p>
            <p className="text-sm text-muted-foreground">
              {distMi.toFixed(2)} mi
              {elevFt > 0 ? ` · ↑ ${elevFt.toLocaleString()} ft` : ""}
            </p>
          </div>

          {breakdown.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {breakdown.map((c) => (
                <Badge
                  key={c.category}
                  variant="outline"
                  className="text-xs font-normal"
                >
                  {c.category} · {c.distance_mi.toFixed(1)}mi · {c.pace}
                </Badge>
              ))}
            </div>
          ) : null}

          <Separator />
          <RunMap activityId={run.activityId} interactive={false} />
        </CardContent>
      </Card>
    </Link>
  );
}
