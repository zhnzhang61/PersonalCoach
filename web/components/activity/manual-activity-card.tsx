"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Dumbbell,
  Footprints,
  Pencil,
  Sparkles,
  Waves,
} from "lucide-react";
import { apiDelete, apiPut } from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { fmtDate } from "@/lib/format";
import type { ManualActivity, ManualActivityType } from "@/lib/types";
import {
  ManualActivityForm,
  type ManualActivityFormValues,
} from "@/components/activity/manual-activity-form";

const TYPE_META: Record<string, { label: string; Icon: typeof Footprints }> = {
  run: { label: "Run", Icon: Footprints },
  swim: { label: "Swim", Icon: Waves },
  gym: { label: "Gym", Icon: Dumbbell },
  // Sparkles instead of MoreHorizontal — three dots on the left of the card
  // looked like an overflow-menu affordance and confused the user into
  // clicking it for an edit menu (there wasn't one). Sparkles is generic
  // "miscellaneous" and unmistakably decorative.
  other: { label: "Other", Icon: Sparkles },
};

const KNOWN_TYPES: ManualActivityType[] = ["run", "swim", "gym", "other"];

export function ManualActivityCard({ activity }: { activity: ManualActivity }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);

  const meta = TYPE_META[activity.type] ?? {
    label: activity.type,
    Icon: Sparkles,
  };
  const Icon = meta.Icon;

  const updateMut = useMutation({
    mutationFn: (payload: ManualActivityFormValues) =>
      apiPut<{ ok: boolean; activity: ManualActivity }>(
        `/api/manual-activities/${encodeURIComponent(activity.id)}`,
        payload as unknown as Record<string, unknown>,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["manual-activities"] });
      setEditing(false);
    },
  });

  const deleteMut = useMutation({
    mutationFn: () =>
      apiDelete<{ ok: boolean }>(
        `/api/manual-activities/${encodeURIComponent(activity.id)}`,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["manual-activities"] });
      // No need to reset editing; the card unmounts when the list refetches.
    },
  });

  if (editing) {
    // Coerce stored type to the canonical set (file may have legacy free-form
    // strings from the dashboard's old aux entries).
    const safeType: ManualActivityType = (KNOWN_TYPES as string[]).includes(
      activity.type,
    )
      ? (activity.type as ManualActivityType)
      : "other";

    return (
      <Card>
        <CardContent className="p-4">
          <ManualActivityForm
            title="Edit activity"
            saveLabel="Save"
            initial={{
              date: activity.date,
              type: safeType,
              description: activity.desc ?? "",
              duration_min: activity.duration_min,
              distance_mi: activity.distance_mi,
            }}
            pending={updateMut.isPending}
            deletePending={deleteMut.isPending}
            error={
              (updateMut.isError && (updateMut.error as Error).message) ||
              (deleteMut.isError && (deleteMut.error as Error).message) ||
              undefined
            }
            onSubmit={(v) => updateMut.mutate(v)}
            onDelete={() => deleteMut.mutate()}
            onCancel={() => setEditing(false)}
          />
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardContent className="flex items-start gap-3 p-4">
        <div className="flex size-10 shrink-0 items-center justify-center rounded-md bg-muted/60">
          <Icon className="size-5 text-muted-foreground" aria-hidden />
        </div>
        <div className="min-w-0 flex-1 space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm font-semibold">{meta.label}</span>
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground">
                {fmtDate(activity.date, "EEE MMM d")}
              </span>
              <button
                type="button"
                onClick={() => setEditing(true)}
                className="rounded-md border border-border bg-background p-1.5 text-muted-foreground transition-colors hover:bg-muted/40 hover:text-foreground"
                aria-label="Edit activity"
              >
                <Pencil className="size-3.5" />
              </button>
            </div>
          </div>
          {(activity.distance_mi != null || activity.duration_min != null) && (
            <div className="flex flex-wrap gap-1.5">
              {activity.distance_mi != null && (
                <Badge variant="outline" className="text-[10px] font-normal">
                  {activity.distance_mi.toFixed(2)} mi
                </Badge>
              )}
              {activity.duration_min != null && (
                <Badge variant="outline" className="text-[10px] font-normal">
                  {activity.duration_min} min
                </Badge>
              )}
            </div>
          )}
          {activity.desc && (
            <p className="text-xs text-muted-foreground">{activity.desc}</p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
