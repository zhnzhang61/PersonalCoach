"use client";

import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  CircleMarker,
  MapContainer,
  Polyline,
  TileLayer,
  useMap,
} from "react-leaflet";
import "leaflet/dist/leaflet.css";

import { apiGet } from "@/lib/api";
import { Skeleton } from "@/components/ui/skeleton";
import type { LatLng, RouteResponse } from "@/lib/types";

// Fits the map viewport to the route's bounding box on first render. Doing it
// inside a child of MapContainer is the canonical react-leaflet pattern —
// useMap() only resolves once the map instance exists.
function FitBounds({ points }: { points: LatLng[] }) {
  const map = useMap();
  useEffect(() => {
    if (points.length === 0) return;
    map.fitBounds(points as [number, number][], { padding: [16, 16] });
  }, [map, points]);
  return null;
}

export function RunMap({ activityId }: { activityId: number }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["runs", activityId, "route"],
    queryFn: () =>
      apiGet<RouteResponse>(`/api/runs/${activityId}/route?max_points=500`),
    staleTime: Infinity,
    retry: false,
  });

  if (isLoading) {
    return <Skeleton className="h-64 w-full" />;
  }
  if (isError || !data) {
    return (
      <div className="flex h-32 items-center justify-center rounded-md border border-border bg-muted/30 text-xs text-muted-foreground">
        No GPS route for this run.
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-md border border-border">
      <MapContainer
        center={data.start}
        zoom={14}
        scrollWheelZoom={false}
        style={{ height: "16rem", width: "100%" }}
        attributionControl={false}
      >
        <TileLayer
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
          // Attribution lives in the page footer / about; OSM ToS for
          // non-commercial personal use accepts that as long as it's
          // discoverable somewhere. Keep the map chrome clean.
        />
        <Polyline
          positions={data.polyline}
          pathOptions={{
            color: "var(--chart-2)",
            weight: 3,
            opacity: 0.85,
          }}
        />
        <CircleMarker
          center={data.start}
          radius={6}
          pathOptions={{
            color: "var(--chart-1)",
            fillColor: "var(--chart-1)",
            fillOpacity: 1,
            weight: 2,
          }}
        />
        <CircleMarker
          center={data.end}
          radius={6}
          pathOptions={{
            color: "var(--chart-2)",
            fillColor: "var(--chart-2)",
            fillOpacity: 1,
            weight: 2,
          }}
        />
        <FitBounds points={data.polyline} />
      </MapContainer>
    </div>
  );
}
