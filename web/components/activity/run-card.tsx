"use client";

import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { fmtDate } from "@/lib/format";
import type { RunActivity } from "@/lib/types";

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

  return (
    <Card>
      <CardContent className="flex flex-col gap-3 p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h3 className="truncate text-sm font-semibold">{name}</h3>
            <p className="text-xs text-muted-foreground">
              {dateStr ? fmtDate(dateStr, "EEE MMM d") : "—"} ·{" "}
              {distMi.toFixed(2)} mi · {pace} /mi
            </p>
          </div>
          {run.averageHR ? (
            <Badge variant="outline" className="shrink-0 text-[10px]">
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
                className="text-[10px] font-normal"
              >
                {c.category} · {c.distance_mi.toFixed(1)}mi · {c.pace}
              </Badge>
            ))}
          </div>
        ) : null}

        {meta.notes ? (
          <p className="text-xs text-muted-foreground">{meta.notes}</p>
        ) : null}

        {elevFt > 0 ? (
          <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
            ↑ {elevFt.toLocaleString()} ft
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}
