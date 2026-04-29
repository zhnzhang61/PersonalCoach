"use client";

import { Dumbbell, Footprints, MoreHorizontal, Waves } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { fmtDate } from "@/lib/format";
import type { ManualActivity } from "@/lib/types";

const TYPE_META: Record<string, { label: string; Icon: typeof Footprints }> = {
  run: { label: "Run", Icon: Footprints },
  swim: { label: "Swim", Icon: Waves },
  gym: { label: "Gym", Icon: Dumbbell },
  other: { label: "Other", Icon: MoreHorizontal },
};

export function ManualActivityCard({ activity }: { activity: ManualActivity }) {
  const meta = TYPE_META[activity.type] ?? {
    label: activity.type,
    Icon: MoreHorizontal,
  };
  const Icon = meta.Icon;

  return (
    <Card>
      <CardContent className="flex items-start gap-3 p-4">
        <div className="flex size-10 shrink-0 items-center justify-center rounded-md bg-muted/60">
          <Icon className="size-5 text-muted-foreground" aria-hidden />
        </div>
        <div className="min-w-0 flex-1 space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm font-semibold">{meta.label}</span>
            <span className="text-xs text-muted-foreground">
              {fmtDate(activity.date, "EEE MMM d")}
            </span>
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
