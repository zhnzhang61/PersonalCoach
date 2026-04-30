"use client";

import { useState } from "react";
import { ChevronDown, ChevronUp, ClipboardEdit } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { fmtDate } from "@/lib/format";
import type { RunActivity } from "@/lib/types";
import { EditRunForm } from "@/components/activity/edit-run-form";
import { TelemetryCharts } from "@/components/activity/telemetry-charts";
import { WeatherStrip } from "@/components/activity/weather-strip";

function metersToMi(m?: number): number {
  return (m ?? 0) / 1609.34;
}

function secToPace(seconds: number, miles: number): string {
  if (seconds <= 0 || miles <= 0) return "—";
  const dec = seconds / 60 / miles;
  const min = Math.floor(dec);
  const sec = Math.floor((dec - min) * 60);
  return `${min}:${sec.toString().padStart(2, "0")}`;
}

export function RunCard({ run }: { run: RunActivity }) {
  const meta = run.manual_meta ?? {};
  const name = meta.name || run.activityName || "Run";
  const dateStr = run.startTimeLocal?.slice(0, 10);
  const distMi = metersToMi(run.distance);
  const durSec = run.movingDuration || run.duration || 0;
  const pace = secToPace(durSec, distMi);
  const elevFt = Math.round((run.elevationGain ?? 0) * 3.281);
  const breakdown = meta.category_stats ?? [];
  // Charts + weather are basic info — always shown. Notes / lap-effort
  // editing lives one click away under "Efforts & Coaching" so the card
  // doesn't feel like a form on first glance.
  const [editorOpen, setEditorOpen] = useState(false);

  return (
    <Card>
      <CardContent className="flex flex-col gap-3 p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h3 className="truncate text-base font-semibold">{name}</h3>
            <p className="text-sm text-muted-foreground">
              {dateStr ? fmtDate(dateStr, "EEE MMM d") : "—"} ·{" "}
              {distMi.toFixed(2)} mi · {pace} /mi
            </p>
          </div>
          {run.averageHR ? (
            <Badge variant="outline" className="shrink-0 text-xs">
              {run.averageHR} bpm
            </Badge>
          ) : null}
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

        {meta.notes ? (
          <p className="text-sm text-muted-foreground">{meta.notes}</p>
        ) : null}

        {elevFt > 0 ? (
          <p className="text-xs uppercase tracking-wide text-muted-foreground">
            ↑ {elevFt.toLocaleString()} ft
          </p>
        ) : null}

        <Separator />
        <WeatherStrip activityId={run.activityId} />
        <TelemetryCharts activityId={run.activityId} />

        <Separator />
        <button
          type="button"
          onClick={() => setEditorOpen((v) => !v)}
          className="flex items-center justify-center gap-1.5 rounded-md border border-border bg-background py-2 text-sm font-medium text-foreground transition-colors hover:bg-muted/40"
          aria-expanded={editorOpen}
        >
          <ClipboardEdit className="size-4" />
          Efforts & Coaching
          {editorOpen ? (
            <ChevronUp className="size-4" />
          ) : (
            <ChevronDown className="size-4" />
          )}
        </button>

        {editorOpen && (
          <EditRunForm run={run} onClose={() => setEditorOpen(false)} />
        )}
      </CardContent>
    </Card>
  );
}
