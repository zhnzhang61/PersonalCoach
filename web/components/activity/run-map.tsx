"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Map, { Layer, Marker, Source, type MapRef } from "react-map-gl/mapbox";
import "mapbox-gl/dist/mapbox-gl.css";
import { Maximize2, X } from "lucide-react";

import { apiGet } from "@/lib/api";
import { Skeleton } from "@/components/ui/skeleton";
import type { RouteResponse } from "@/lib/types";

const MAPBOX_TOKEN = process.env.NEXT_PUBLIC_MAPBOX_TOKEN;

// Three styles I'd actually toggle between for a run: street view for daily
// city loops, outdoors for trails (shaded relief + topo lines), satellite
// for "where exactly was that loop". Outdoors is the default — pleasant
// palette for both city and trail.
const STYLES = [
  { id: "outdoors-v12", label: "Outdoor" },
  { id: "streets-v12", label: "Street" },
  { id: "satellite-streets-v12", label: "Satellite" },
] as const;

type StyleId = (typeof STYLES)[number]["id"];

// Strava-ish accent that's readable on every style (street, outdoors,
// satellite). Hex rather than CSS var — mapbox-gl paint expressions are
// canvas-side, they don't read CSS custom properties.
const ROUTE_COLOR = "#fc4c02";
const START_COLOR = "#16a34a";
const END_COLOR = "#dc2626";

export function RunMap({
  activityId,
  interactive = true,
}: {
  activityId: number;
  interactive?: boolean;
}) {
  const [styleId, setStyleId] = useState<StyleId>("outdoors-v12");
  const [fullscreen, setFullscreen] = useState(false);
  const mapRef = useRef<MapRef>(null);

  // While fullscreen, kill page scroll and let Escape close it. react-map-gl's
  // built-in ResizeObserver doesn't always catch a parent class change on the
  // same frame (especially on iOS Safari, where the visible viewport changes
  // around the URL bar), so we explicitly poke map.resize() across two frames
  // to make sure the canvas fills the new container.
  useEffect(() => {
    const id1 = requestAnimationFrame(() =>
      requestAnimationFrame(() => mapRef.current?.resize()),
    );
    if (!fullscreen) {
      return () => cancelAnimationFrame(id1);
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFullscreen(false);
    };
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKey);
    return () => {
      cancelAnimationFrame(id1);
      document.body.style.overflow = prev;
      window.removeEventListener("keydown", onKey);
    };
  }, [fullscreen]);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["runs", activityId, "route"],
    queryFn: () =>
      apiGet<RouteResponse>(`/api/runs/${activityId}/route?max_points=500`),
    staleTime: Infinity,
    retry: false,
  });

  // GeoJSON expects [lon, lat] order; our API returns [lat, lon].
  const routeGeoJson = useMemo(() => {
    if (!data) return null;
    return {
      type: "Feature" as const,
      geometry: {
        type: "LineString" as const,
        coordinates: data.polyline.map(([lat, lon]) => [lon, lat]),
      },
      properties: {},
    };
  }, [data]);

  const initialViewState = useMemo(() => {
    if (!data) return undefined;
    const b = data.bounds;
    if (
      b.min_lat != null &&
      b.max_lat != null &&
      b.min_lon != null &&
      b.max_lon != null
    ) {
      return {
        bounds: [
          [b.min_lon, b.min_lat],
          [b.max_lon, b.max_lat],
        ] as [[number, number], [number, number]],
        fitBoundsOptions: { padding: 30 },
      };
    }
    return {
      latitude: data.start[0],
      longitude: data.start[1],
      zoom: 13,
    };
  }, [data]);

  if (!MAPBOX_TOKEN) {
    return (
      <div className="flex h-32 items-center justify-center rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-300">
        NEXT_PUBLIC_MAPBOX_TOKEN missing — add it to the project .env.
      </div>
    );
  }

  if (isLoading) {
    return <Skeleton className="h-64 w-full" />;
  }
  if (isError || !data || !routeGeoJson) {
    return (
      <div className="flex h-32 items-center justify-center rounded-md border border-border bg-muted/30 text-xs text-muted-foreground">
        No GPS route for this run.
      </div>
    );
  }

  // Satellite styles look best without terrain shading (the imagery already
  // shows topology); other styles get a gentle 3D push for trail context.
  const useTerrain = styleId !== "satellite-streets-v12";

  const wrapperClass = fullscreen
    ? "fixed inset-0 z-50 bg-background"
    : "relative overflow-hidden rounded-md border border-border";
  const wrapperStyle = fullscreen ? undefined : { height: "16rem" };

  return (
    <div className={wrapperClass} style={wrapperStyle}>
      <Map
        ref={mapRef}
        mapboxAccessToken={MAPBOX_TOKEN}
        initialViewState={initialViewState}
        mapStyle={`mapbox://styles/mapbox/${styleId}`}
        style={{ width: "100%", height: "100%" }}
        attributionControl={false}
        interactive={interactive}
        terrain={useTerrain ? { source: "mapbox-dem", exaggeration: 1.2 } : undefined}
      >
        <Source
          id="mapbox-dem"
          type="raster-dem"
          url="mapbox://mapbox.mapbox-terrain-dem-v1"
          tileSize={512}
          maxzoom={14}
        />
        <Source id="route" type="geojson" data={routeGeoJson}>
          <Layer
            id="route-casing"
            type="line"
            paint={{
              "line-color": "#ffffff",
              "line-width": 6,
              "line-opacity": 0.6,
            }}
            layout={{ "line-cap": "round", "line-join": "round" }}
          />
          <Layer
            id="route-line"
            type="line"
            paint={{
              "line-color": ROUTE_COLOR,
              "line-width": 3.5,
              "line-opacity": 0.95,
            }}
            layout={{ "line-cap": "round", "line-join": "round" }}
          />
        </Source>
        <Marker longitude={data.start[1]} latitude={data.start[0]} anchor="center">
          <div
            className="size-3 rounded-full border-2 border-white shadow"
            style={{ backgroundColor: START_COLOR }}
            aria-label="Start"
          />
        </Marker>
        <Marker longitude={data.end[1]} latitude={data.end[0]} anchor="center">
          <div
            className="size-3 rounded-full border-2 border-white shadow"
            style={{ backgroundColor: END_COLOR }}
            aria-label="End"
          />
        </Marker>
      </Map>

      {/*
        Fullscreen pushes overlay buttons below the iPhone Dynamic Island /
        status bar via safe-area-inset-top. Inline mode sits inside the run
        card's normal padding, so 0.5rem is fine there.

        Preview-card map renders non-interactive (no style/fullscreen chrome)
        so the wrapping Link captures all taps cleanly.
      */}
      {interactive && (
      <div
        className="absolute right-2 flex gap-1 rounded-md bg-background/90 p-1 shadow-sm backdrop-blur"
        style={{
          top: fullscreen
            ? "calc(env(safe-area-inset-top) + 0.5rem)"
            : "0.5rem",
        }}
      >
        {STYLES.map((s) => (
          <button
            key={s.id}
            type="button"
            onClick={() => setStyleId(s.id)}
            className={
              "rounded px-2 py-1 text-[11px] font-medium transition-colors " +
              (s.id === styleId
                ? "bg-foreground text-background"
                : "text-muted-foreground hover:text-foreground")
            }
          >
            {s.label}
          </button>
        ))}
      </div>
      )}

      {interactive && (
      <button
        type="button"
        onClick={() => setFullscreen((v) => !v)}
        className="absolute rounded-md bg-background/90 p-1.5 text-foreground shadow-sm backdrop-blur transition-colors hover:bg-background"
        style={{
          top: fullscreen
            ? "calc(env(safe-area-inset-top) + 0.5rem)"
            : "0.5rem",
          left: fullscreen
            ? "calc(env(safe-area-inset-left) + 0.875rem)"
            : "0.5rem",
        }}
        aria-label={fullscreen ? "Exit fullscreen" : "Open fullscreen map"}
      >
        {fullscreen ? <X className="size-5" /> : <Maximize2 className="size-4" />}
      </button>
      )}
    </div>
  );
}
