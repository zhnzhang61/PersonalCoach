"use client";

import { useState } from "react";
import { Pencil } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { fmtDate } from "@/lib/format";
import type { RunActivity } from "@/lib/types";
import { EditRunForm } from "@/components/activity/edit-run-form";

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
  const [editing, setEditing] = useState(false);

  if (editing) {
    return <EditRunForm run={run} onClose={() => setEditing(false)} />;
  }

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
          <div className="flex shrink-0 items-center gap-2">
            {run.averageHR ? (
              <Badge variant="outline" className="text-[10px]">
                {run.averageHR} bpm
              </Badge>
            ) : null}
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="rounded-md border border-border bg-background p-1.5 text-muted-foreground transition-colors hover:bg-muted/40 hover:text-foreground"
              aria-label="Edit run"
            >
              <Pencil className="size-3.5" />
            </button>
          </div>
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
