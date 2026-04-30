"use client";

import { useQuery } from "@tanstack/react-query";
import { CloudOff, Droplets, Thermometer } from "lucide-react";
import { apiGet } from "@/lib/api";
import type { WeatherSnapshot } from "@/lib/types";
import { Skeleton } from "@/components/ui/skeleton";

export function WeatherStrip({ activityId }: { activityId: number }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["runs", activityId, "weather"],
    queryFn: () =>
      apiGet<WeatherSnapshot>(`/api/runs/${activityId}/weather`),
    // Weather is immutable once a run is in the past; cache aggressively.
    staleTime: Infinity,
    retry: false,
  });

  if (isLoading) {
    return <Skeleton className="h-9 w-full" />;
  }

  if (isError || !data) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
        <CloudOff className="size-3.5 shrink-0" aria-hidden />
        <span>Weather unavailable for this run.</span>
      </div>
    );
  }

  const tempF = data.temperature_f;
  const apparentF = data.apparent_temperature_f;
  const humidity = data.humidity_pct;
  const dewF = data.dew_point_f;
  const showFeels = apparentF != null && tempF != null && Math.abs(apparentF - tempF) >= 2;

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border bg-muted/30 px-3 py-2 text-xs">
      <span className="flex items-center gap-1.5 text-foreground">
        <Thermometer className="size-3.5 text-muted-foreground" aria-hidden />
        <span className="tabular-nums font-medium">
          {tempF != null ? `${tempF.toFixed(0)}°F` : "—"}
        </span>
        {showFeels && (
          <span className="text-muted-foreground">
            (feels {apparentF.toFixed(0)}°F)
          </span>
        )}
      </span>
      <span className="flex items-center gap-1.5 text-foreground">
        <Droplets className="size-3.5 text-muted-foreground" aria-hidden />
        <span className="tabular-nums font-medium">
          {humidity != null ? `${humidity}%` : "—"}
        </span>
        <span className="text-muted-foreground">humidity</span>
      </span>
      {dewF != null && (
        <span className="text-muted-foreground">dew point {dewF.toFixed(0)}°F</span>
      )}
      <span className="ml-auto text-[10px] uppercase tracking-wide text-muted-foreground/60">
        Open-Meteo
      </span>
    </div>
  );
}
